# Wave-23 — Writer Convergence Plan

**Status:** plan (evidence complete, awaiting green-light)
**Date:** 2026-09-06
**Evidence base:** 5 parallel forensic investigations (local job 343, prod jobs 373/374/375,
static writer-gate map, LLM call-path/hang analysis, 145-job prod failure census).

---

## 1. Executive summary

The writer failure class is the dominant prod failure mode **today** (class A "time deaths" +
class B "no-converge" = 47% of the latest job epoch 340–377; writer-adjacent classes were
95.3% of epoch 150–249). Five independent investigations converge on one causal spine:

> **The writer does not die of incompetence — it dies of delivery failure.** It burns its
> wall clock on invisible LLM hangs and context-starved re-reading, and the harness has no
> per-call mechanism to notice, retry, or reroute. When a draft lands before the reaper, the
> job usually completes (ego 373, katespade 375); when it doesn't, the job fails (local 343).

Every completed prod job in the cohort paid the disease anyway: **both** ego 373 and
katespade 375 had a code_writer invocation killed at 1800s with a thread abandoned —
a 30-minute tax per occurrence that currently decides pass vs fail by luck of timing.

## 2. Root causes (ranked by measured impact)

### RC-1 — LLM no-chunk hang is invisible to every guard (biggest lever)

- `ClassifiedRetryChatOpenAI._stream` fully buffers the stream (`chunks = list(super()._stream(...))`,
  `webapp/agents/llm.py:462-484`) — consumers see zero chunks until the whole generation
  finishes, and `record_failure`/`record_success` only run after consumption completes or raises.
- A call producing no chunks and raising nothing reaches neither: the breaker can't count it,
  the classified retry ladder can't ride it, and `CircuitBreakerCallback` is documented dead
  code on langchain-core 1.5.1+.
- The only bound is the phase wall-clock reaper (`AGENT_INVOKE_TIMEOUT`, 1800s in prod) —
  granularity = the whole phase, the abandoned thread leaks, and the fallback model is never
  consulted (breaker swap reads at `get_llm` build time only).
- `LLM_REQUEST_TIMEOUT`/`CODE_WRITER_LLM_TIMEOUT` (300s/600s) demonstrably did NOT bound the
  observed 1800s zero-output window (local 343 attempt 2: 1800s, only heartbeats + 16 late
  tool calls; final 12m23s fully silent).

### RC-2 — Context starvation drives a re-reading spiral (the convergence delta)

Measured contrast, successful 341 vs failed 343 (local) and 374 (prod):

| Metric | Success (341) | Failure (343 / 374) |
|---|---|---|
| read_file calls per invocation | 7 | 48+16 (343, both attempts) / 123 (374) |
| shared-module reads | 2 total, unpaged | 22 in attempt 1 alone, 17 with offset |
| scraper_draft.py bytes | full draft by call 17 | 0 bytes in 64 calls (343); fix rounds = 0 writes (374) |
| write:read ratio | 13:7 | 3:29 then 3:10 |

- **Mechanism (verified this session):** `LLM_TRUNCATION_PER_MSG_CAP = 8000`
  (`webapp/agents/subagents.py:805`) re-truncates every non-seed message to 8K chars in what
  the model sees on later turns. A 50K `read_file` page (the tool's own page size,
  `filesystem_tools.py:321`) collapses to 8K in-context — the model literally reports
  "The reads are being truncated to 8K chars each. Let me page through systematically"
  (374, SessionLog) and pages at ~8K strides (observed offsets 7900/8000/13500), including
  past-EOF reads (offset 33000 vs 21,274-char file) and wrap-around re-reads from offset 0.
- First-write latency: success jobs write within their first ~7 calls; failures write first
  at call 43+ or never. ~63% of a failed attempt's wall clock is LLM thinking latency
  (243s before first tool; silent turns of 335s/262s/150s), not tool execution.

### RC-3 — No draft-first forcing function for code_writer

- `_run_budgeted_agent`'s auto-extend "write NOW" nudge exists (`graph.py:2414-2444`) but
  code_writer never passes through it (only site_analyzer/product_analyzer do).
- The writer prompt's draft-first workflow is prose-only (`.opencode/agents/code-writer.md`);
  nothing deterministic intervenes when calls climb with zero draft bytes.
- Prod 374's two fix rounds after FAIL verdicts: 99 tool calls, **100% read/search, zero
  writes** — the writer never delivered a single change, then gave up ("Sorry, need more
  steps…") while still planning "then write the scratch test."

### RC-4 — Instruction contradiction at the fix loop (374-specific, hypothesis)

The injected `[CONTEXT]` template-fidelity guard orders "Do NOT re-signature or redefine the
template's discovery/pagination helpers … CONTRACT, not boilerplate" while the tester's
remediation for the actual defect requires replacing exactly that subsystem. The writer was
caught between contradictory instructions for 33 minutes and produced nothing.

### RC-5 — Observability gaps that hid all of the above

- `/tool-calls/` drops abandoned invocations wholesale (persister `graph.py:7447-7467` only
  runs on normal exit): 374 shows 150 writer rows vs 176 real; 375 loses 26 — including all
  mutation rows of 374's invocation 2.
- The identical-draft freeze gate's hash comparison is unobservable (celery stdout only);
  `test_report.json` is not downloadable; `ToolCallLog` timestamps are flush-time, not
  call-time; `SessionLog.seq` is not unique.
- One anomaly never explained: 343 attempt 2's third `run_scraper` dispatch produced no
  `[RUN_SCRAPER]`/`[EXEC-ALIVE]` rows at all (its two predecessors each produced them within
  ~50ms) — a possible silent wedge in the dispatch path.

## 3. Fixes (ranked)

### W23-1 — Per-LLM-call no-chunk watchdog + breaker feeding (targets RC-1)

**Seam (single best location):** inside `ClassifiedRetryChatOpenAI._stream` and `_astream`
(`webapp/agents/llm.py:462-506`). This is the only place chunk arrival is observable; the
breaker/retry plumbing is 3 lines away; a raised transient-class exception rides the existing
classified ladder and feeds the existing breaker → existing fallback reroute.

- Consume `super()._stream(...)` incrementally (buffering contract unchanged) with a timer:
  if no chunk arrives within `LLM_NO_CHUNK_TIMEOUT` (new env, default 120s; code_writer may
  want 180s given long thinking turns), raise a transient-class exception
  (`openai.APITimeoutError`-family, already in `_TRANSIENT_ERRORS`).
- Trip N (2) consecutive no-chunk failures → `llm_breaker.record_failure` so the NEXT
  `get_llm` swaps to the fallback model; log a SessionLog row per trip (new
  `[LLM-NO-CHUNK]` marker) so the phase log shows why.
- Apply identically to `_astream` so the async lane doesn't become the new blind spot.

**TDD outline:**
1. Unit: fake generator that sleeps > timeout → assert watchdog raises transient error and
   `_retry_classified_sync` retries, then breaker counter increments on exhaustion.
2. Unit: healthy slow-but-streaming generator (chunks < timeout apart) → no trip.
3. Unit: `_astream` twin parity test.
4. Regression: existing llm retry/breaker tests stay green.

**Risk:** false trips on legitimately slow first tokens for big codegen prompts. Mitigate:
default generous (120–180s), env-tunable, and trip requires *zero* chunks (not slow chunks).

### W23-2 — Draft-first forcing function for code_writer (targets RC-3)

- Deterministic intervention inside the writer's tool context: after
  `CODE_WRITER_DRAFT_NUDGE_CALLS` (new env, default 12) tool calls with zero
  `write_file`/`edit_file` against `scraper_draft.py`, inject a hard directive into the next
  model turn: "Write scraper_draft.py NOW — a minimal draft beats more research. You are at
  call N of M."
- Reuse the auto-extend nudge pattern (`graph.py:2414-2444`); wire it into the writer's
  invocation path (which currently bypasses `_run_budgeted_agent`).
- Optionally pair with a probe-budget: after the nudge fires, block further probe-family
  `run_scraper` dispatches until a draft exists (341's winning pattern was write-first).

**TDD outline:**
1. Unit: counter over tool-context calls; nudge string appears in state/messages at
   threshold; not before.
2. Unit: a write to scraper_draft.py resets the counter and stops the nudge.
3. Unit: probe-family run_scraper blocked post-nudge pre-draft; allowed after draft.

### W23-3 — Kill the 8K context-starvation spiral (targets RC-2)

- Raise `LLM_TRUNCATION_PER_MSG_CAP` for **tool result messages** specifically (e.g. 24K;
  keep 8K for assistant/other) — read_file's 50K page should survive in-context, or at least
  the model must be told precisely which slice it is seeing.
- Alternative/complement: teach `read_file` to return a bounded, high-density view for
  known shared modules (signatures + docstrings via AST) so full-source paging is never
  needed; and add a same-file re-read guard (warn at 3, block at 5 identical
  (path, offset-range) reads with a "you already have this content" message).

**TDD outline:**
1. Unit: `_truncate_messages` keeps tool messages to the new cap while others stay at 8K.
2. Unit: read_file AST summary mode returns signatures for `src/http_fetch.py`-shaped input.
3. Unit: re-read guard counts (path, offset) overlaps; blocks with instructive message.

**Risk:** bigger tool messages re-inflate context (the balloon `code-writer-context-ballooning`
memory warns about). The determinstic truncation budget (`LLM_TRUNCATION_MAX_CHARS=180K`)
still bounds the whole prompt; measure before/after with the wave-15 harness.

### W23-4 — Resolve the guard-vs-remediation contradiction (targets RC-4)

- Scope the template-fidelity `[CONTEXT]` guard to non-discovery helpers, or append an
  override clause: "Exception: when the tester's remediation targets discovery, replacing
  the discovery-source functions IS allowed — keep their call signatures."

**TDD outline:** unit on the message builder: remediation containing "discovery" produces
the override clause; otherwise the guard text is unchanged.

### W23-5 — Observability (targets RC-5; cheap, do alongside)

1. Persist wall-clock kills to ToolCallLog at reap time (currently only SessionLog
   `[INVOKE-TIMEOUT]`) — restores `/tool-calls/` fidelity for abandoned invocations.
2. Log the freeze-gate hash comparison (`graph.py:5298-5311`) as a SessionLog row, not just
   celery stdout.
3. Serve `test_report.json` via the output download route (register in run_history).
4. Investigate the silent third `run_scraper` dispatch (343) — no `[RUN_SCRAPER]`/`[EXEC-ALIVE]`
   rows; check the dispatch path for a wedge (may connect to browser-service exec threads).

### W23-6 — Ops notes (no code)

- Census classes D (11%) and E (15.9% ex-incident) are NOT writer problems; don't let
  wave-23 scope creep into discovery/execution.
- Jobs 350–357: one batch re-drive shipped no seeds (9 failures in 2s each) — intake guard
  opportunity, separate from wave-23.
- Job 125 (fanatics): watchdog revoked a job that had already extracted 10 items — watchdog
  activity check may need the same `[HEARTBEAT]`-exclusion treatment inverted (don't count
  extraction silence as wedged when products exist).

## 4. Sequencing

1. **W23-1 + W23-5.1/.2** first — they attack the dominant current failure mode (class A
   time deaths = 6 of 17 latest-epoch failures) and are low-risk.
2. **W23-2 + W23-3** — the convergence pair; validate together with a local A/B
   (jomashop-style list_page job) using the wave-15 harness.
3. **W23-4** — small, ship with W23-2.
4. Re-census after a prod batch to confirm class A/B shrink.

## 5. Non-goals

- No change to AGENT_RECURSION_MAP / iteration caps (the code-writer-context-ballooning
  memory established caps are the wrong lever).
- No LLM_ASYNC_EXECUTION flip (pinned off; sync lane is the tested path).
- No discovery-phase changes in this wave.
