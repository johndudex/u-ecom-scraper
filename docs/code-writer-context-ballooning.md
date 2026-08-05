# code_writer Context Ballooning — Root Cause & Remedy Analysis

> Status: **Analysis complete; not yet implemented.** Revisit when prioritizing codegen latency/reliability.
> Origin: production job 250 (zquiet.com), 2026-08-04. 6-agent deep dive + remedy critique.
> Supersedes parts of [`code-writer-summarization-plan.md`](./code-writer-summarization-plan.md) (whose `SummarizationMiddleware` half never shipped and is **not** the recommended fix — see §6).

## TL;DR

Job 250 succeeded (14 products) but took **~23 min** because `code_writer` ballooned its context to **~707K chars / 122 messages within a single invocation** (each invocation starts from a fresh 2-message seed — this is **not** cross-retry accumulation). Four gaps converge, all centered on code_writer:

1. **No conversation summarization exists** — the old `headroom.compress` hook was removed (`3b5ebf6`); its replacement (`SummarizationMiddleware`) was designed but never shipped.
2. **Per-turn amnesia** — the truncator keeps only the last 6 messages once over budget, below code_writer's ~25-step unit of work → it forgets what it tried → re-derives → regrows.
3. **`tool_calls` are invisible to the budget and untrimmed** — `write_file`/`edit_file` args (the ~30K scraper) are never measured or trimmed.
4. **Nothing terminates the loop** — real cap is `recursion_limit=120`; `AGENT_MAX_ITERATIONS=20` is dead code; code_writer bypasses the budget machinery.

**The recommended fix is NOT summarization and NOT iteration caps.** It is **behavioral** (stop code_writer re-emitting the whole file every turn + detect convergence) plus **cheap deterministic state-shaping** (retain the seed message + a last-error stub). See §5–§7.

## ⚠️ CRITICAL FINDING (2026-08-05 follow-up investigation) — the truncator is a runtime NO-OP

An attempt to ship the "safe subset" (honest `_clen` + seed retention) on an `optimisation` branch, plus a 3-agent adversarial review, surfaced a load-bearing fact that **reframes this entire analysis**:

**`_truncate_messages` does NOT actually shrink what the LLM sees.** It is wired as a langgraph `pre_model_hook` and returns `{"messages": kept}`. Per the langgraph contract (`langgraph/prebuilt/chat_agent_executor.py:396-421`, verified in the running container), a pre-model hook that returns `messages` is merged into state via the `add_messages` reducer — which **appends/updates-by-ID, never replaces** unless you return `[RemoveMessage(id=REMOVE_ALL_MESSAGES), *new]`. `_get_model_input_state` then reads `state["messages"]` (post-merge). Concretely:
- Messages **dropped** by Step 2 are never removed from state → the LLM still sees them.
- Step 1's `_trim` constructs **new no-ID message objects** → they get **appended** (the list *grows*).
- The "Truncated messages: 68 → 7" log lines report what the function **computed**, not what the model **received**.

**Evidence:** langgraph source (above) + an empirical `CapturingModel` test (a >200k input came back to the model at 208k = originals + appended trimmed copy) + the 2026-08-05 local e2e (adameve job 200): `code_writer` ballooned to **446k chars / 68 messages** with the new `seed_retained=True` log firing repeatedly — and each LLM call was slow (minutes), consistent with the model receiving the **full** 446k, not a bounded ~50k view.

**Consequence — the "amnesia" framing (Gap 2) is wrong.** The LLM was never losing the seed or recent turns; it was receiving the **full ballooned context every call**. So the ballooning is a pure **latency/cost** problem (huge per-call input → slow, expensive LLM calls), **not** an amnesia/correctness problem. (Consistent with: job 250 succeeded with 14 products despite the ballooning.)

**The "safe subset" (honest `_clen`, seed retention, headroom-skip) are therefore no-ops too** — they improve the *computed* list that the hook discards. They were implemented, unit-tested (8 tests), run through a real `code_writer` (job 200, no crash), and 3× adversarially reviewed (all clean for regressions) — then **reverted** on the `optimisation` branch because shipping no-ops as a fix is misleading. The work is in git history if needed for the real fix.

### The real fix (and why it's NOT a quick change)

Switch the hook's return to the langgraph-blessed mechanism:
```python
return {"llm_input_messages": kept}   # shapes the model input WITHOUT mutating state["messages"]
```
`_get_model_input_state` reads `llm_input_messages` first, so the model sees `kept` while the react loop's accumulation is untouched. **But naive application is dangerous**, so this was NOT shipped autonomously:
1. **Step 1's 8k per-msg cap would go live** → it would cap the **25–35k `build_code_writer_message` seed to 8k** → catastrophic loss of the task/strategy/field-map. The seed MUST be exempted from the per-msg cap (or the cap raised) before this path is enabled.
2. **Pairing**: once Step 2 actually drops messages, the fill loop can **orphan a `ToolMessage`** (keep it while dropping its `AIMessage`) → Z.AI/OpenAI HTTP 400 "an assistant message with 'tool_calls' must be found before a 'tool' message". `_validate_chat_history` only checks AI→Tool, not Tool→AI, so it won't catch it. The drop must be pair-aware (drop ToolMessages whose `tool_call_id` isn't backed by a kept AIMessage).
3. **Broad blast radius**: making Step 1 live affects **every** agent that uses the hook (site_analyzer, product_analyzer, …) — any >8k message (e.g. product_analyzer page snapshots, site_analyzer probe HTML) would be capped for the model. Needs a per-agent audit of large-message needs.
4. **Latency-vs-quality tradeoff**: bounding the view (fast/cheap) risks dropping context the agent needs (quality). The seed-exemption mitigates the biggest risk, but this is a real tradeoff that needs the user's call + staged rollout behind the `LLM_TRUNCATION_MODE='off'` kill-switch.

**Recommended next step (for the user):** implement `llm_input_messages` + seed-exemption + pair-safe drop, default ON with the `LLM_TRUNCATION_MODE='off'` kill-switch as instant rollback, and validate on a ballooning site (zquiet/adameve) comparing both latency AND output quality (record count + field coverage) before/after. This is a real behavior change — do not ship without measuring quality, not just speed.

## Symptom (job 250 evidence)

Celery logs (`ForkPoolWorker-2`), one `code_writer` invocation:
```
Truncated messages: 44 → 6 (was 275463 chars, budget 180000, deterministic)
... input count climbs monotonically every turn ...
Truncated messages: 122 → 6 (was 707967 chars, budget 180000, deterministic)
```
- ~20 LLM calls, ~11 min for pass 1; retry 1 then succeeded with all 14 items.
- `Transform content_router: 2513 -> 2513 tokens (saved 0) [6068.2ms]` — the per-tool-output compressor ran but saved nothing and cost ~6s.

## Root cause — 4 converging gaps

### Gap 1 — No conversation summarization
- `headroom.compress` used to run inside `_truncate_messages` (commit `7d82d89`). **Removed in `3b5ebf6`** for two reasons (documented in the `_truncate_messages` docstring, `subagents.py:535-540`): (a) it made a **sync blocking call inside the only cancellation path**, defeating `asyncio.wait_for` (the P0 precondition of the Per-Phase Execution Contract); (b) it added **run-to-run non-determinism** (a codegen-variance contributor).
- Replacement: deterministic drop-oldest (`subagents.py:532-617`, settings `LLM_TRUNCATION_MODE="deterministic"`, `settings.py:184-189`).
- `SummarizationMiddleware` (proposed in `docs/code-writer-summarization-plan.md`) was **never wired** (`_build_agent`, `subagents.py:686-698`, has no middleware path). Stale comment at `graph.py:2856` still references it.

### Gap 2 — Per-turn amnesia drives the loop
- `_truncate_messages` Step 2 keeps `system_msgs + last _MIN_KEEP_RECENT non-system messages`, `_MIN_KEEP_RECENT = 6` hardcoded (`subagents.py:560`, `:596-611`).
- code_writer's natural workflow is ~25 steps; a 6-message (~3 round-trips) horizon is **below its unit of work**. Once the transcript crosses 180K (within a handful of turns), Step 2 fires *every* turn → the agent forgets the strategy/spec/what-it-already-tried → re-reads files, re-writes the scraper, re-runs tests → transcript regrows 44→122 → re-truncate. **This amnesia→re-derive→regrow loop IS the ballooning.**

### Gap 3 — `tool_calls` invisible to the budget AND untrimmed
- `_clen` (`subagents.py:562-563`) = `len(str(m.content))` only — **does not count `tool_calls`**.
- `_trim` (`subagents.py:565-581`) rewrites only `.content` — **never touches `tool_calls`**.
- Effect: `write_file`/`edit_file` args (the ~30K scraper source) are never measured and never trimmed. The 180K budget under-counts reality; the 6 kept-recent messages carry full untrimmed `tool_calls`.

### Gap 4 — Nothing terminates the loop
- Real cap: `AGENT_RECURSION_MAP["code_writer"] = 120` recursion steps (~60 LLM rounds), `graph.py:585`.
- `AGENT_MAX_ITERATIONS = {"code_writer": 20}` (`subagents.py:109`) is **dead code** — never read (only the definition site matches a grep).
- code_writer calls `_invoke_agent_with_timeout` directly (`graph.py:2874`); it **bypasses `_run_budgeted_agent`** (`graph.py:1318`, whose only callers are site_analyzer/product_analyzer at `:1594,:1679`). So budget-exhaustion / auto-extend / `human_approval` do not apply.
- Only soft cap: `_cap_run_scraper ≤ 3` (`subagents.py:894-911`) — covers `run_scraper` only; `write_file`/`read_file`/`edit_file` are uncapped.

### Secondary inefficiency
- `headroom.compress` is invoked per-tool-output at `shell_tools.py:81`, `web_tools.py:82`, `skill_tools.py:83`, `playwright_tools.py:301`. For code_writer's dense code/JSON it computes `compressed/original ≥ 1.00` → keeps original → **0 saved**, ~6s wasted per call. Verified on prod: onnxruntime loads fine (1.28.0) — the no-op is by-design incompressibility, **not** a broken dep.

## Why other agents don't balloon
- `product_analyzer`, `site_analyzer` write one analysis JSON and exit — no write→test→fix loop.
- `code_tester` has only `read_file`/`write_file`/`run_scraper` (no `edit_file`), writes `test_report.json` once, and uses the v1 `create_agent` path with **no** truncation hook (`subagents.py:499`).
- `code_writer` is structurally unique: it writes a **20–40K Python file**, then iteratively `edit_file`s it guided by `run_scraper` output. File-size × iteration-count × uncounted-`tool_calls` is exclusive to this agent.

## The correct decomposition (A / B / C)

The ballooning is three separable problems. **Summarization and iteration caps each address at most one, and miss the latency driver.**

| | Problem | Harm | Real lever |
|---|---------|------|-----------|
| **A** | Accumulated history grows to 707K | Low — the truncation hook is **view-only** (shapes the model's per-call input; does **not** write back to `state.messages`, which is why it grew 44→122 despite the hook firing). Cost = per-call compress stalls + agent internal memory. | Make `_clen` count `tool_calls`; skip the headroom no-op for code_writer outputs. |
| **B** | Amnesia (keep-last-6 drops task/strategy) | Medium — drives re-derivation, inflates turn count. | **Deterministically** retain the seed `HumanMessage` + a concise "last action / last error" stub in the keep-set. No LLM, no async dependency. |
| **C** | Behavioral rewrite loop — re-emits the whole ~30K file every turn, doesn't converge | **High — this is the bulk of the ~11-min latency.** The big bytes are the *current file*, not historical conversation. | **Behavioral/tooling**: bias toward `edit_file` over `write_file`; add a concise status tool (line count + last error, not the whole file); detect convergence (file unchanged across a `run_scraper` → stop). |

## §6 — Why LLM summarization (the old "Tier 3") is **not** the recommended fix

1. **Hidden massive prerequisite.** It is gated on the entire async-cancellation refactor (Per-Phase Execution Contract Pillar 1) — the phased 0→5 substrate migration that is itself the fix for the dominant failure mode. It is **not independently shippable**; it is downstream of the largest ongoing project.
2. **Reintroduces a removal reason async can't fix.** headroom was removed for (a) sync blocking *and* (b) non-determinism. Async fixes (a), not (b). "Cached" doesn't restore determinism — in a loop the history differs every run, so the content-hash cache misses every time.
3. **Adds LLM calls in the hot path → new failure surface → circular dependency.** A rolling summarizer fires its own LLM call per trigger, creating a new hang/timeout site that itself needs cancellation — the very work it's waiting on.
4. **Not "wire middleware" — a constructor migration.** The plan routes code_writer through `create_agent(..., middleware=[SummarizationMiddleware(...)])` **instead of** the current `create_react_agent` (langchain v1 vs langgraph) — a risky change to the most important agent.
5. **Misdiagnoses.** Summarization bounds history (helps A) and reduces amnesia (helps B, but achievable cheaper/deterministically). It does **not** fix **C** — the agent still does write→run→edit→run→edit for ~20 turns. The loop *length* is behavioral.
6. **Harms reproducibility** — erodes the deterministic-truncation property used for regression reasoning.

Summarization remains a *deferred option* for (A) only — and only worth revisiting **after** async lands, accepting the determinism tax. Even then, fixing (C) likely makes it unnecessary for code_writer.

## §7 — Why the cap-based fixes (the old "Tier 1") break

- **Raise `_MIN_KEEP_RECENT` 6→~25** ❌ — (i) partly a no-op: Step 2's accumulator (`subagents.py:601-610`) keeps recent msgs only while they fit `budget = 180000 − system`; at ≤8000/msg that's ~22 max; (ii) **dangerous**: kept messages carry untrimmed `tool_calls` (30K each) → sending more can exceed GLM's real context window → API 400s; (iii) "per-agent" isn't a one-liner — the hook signature `_truncate_messages(input_dict)` has no agent identity.
- **Lower `recursion_limit` 120→~50** ⚠️ — `GraphRecursionError` *is* caught (`graph.py:104`, in services/tasks), so not a hard crash, but "caught" means code_writer produced nothing that invocation → **burns a strategy retry**; 50 super-steps ≈ 25 LLM rounds is right at the author's "~25 steps for a normal run" cliff → risks cutting off legitimately complex jobs and exhausting the 3 retries. Only bounds the balloon to ~25 rounds (still slow).
- **Total-tool-call guard** ❌ — not a one-liner (needs a cross-tool counter; the existing `_cap_run_scraper` is one-tool via `apply_guard`); and the guard returns a fake string **instead of executing the tool**, so a hard-refuse mid-fix **swallows the agent's actual write/edit** → broken scraper.
- **`### BUDGET:` prompt line** 🟡 — non-binding nudge GLM can ignore; other agents are bounded by workflow shape, not the line; may suppress the valuable self-test loop.

## Recommended path (corrected order)

1. **(C) Behavioral convergence** — the actual latency fix, no prerequisites:
   - Bias code_writer toward `edit_file` (diffs) over `write_file` (full rewrites) in `code-writer.md` + `build_code_writer_message`.
   - Add a concise status tool (current file line count + last `run_scraper` error), so the agent stops re-reading the whole 30K file to find edit anchors.
   - Convergence detection: if `scraper_draft.py` is unchanged across a `run_scraper` cycle, terminate.
2. **(B) Deterministic amnesia fix** — retain the seed `HumanMessage` + a deterministic last-action/last-error stub in the truncator's keep-set (treat like system msgs). No LLM, no async dependency.
3. **(A) Cheap correctness/latency** — make `_clen` count `tool_calls`; skip `headroom.compress` for code_writer's `run_scraper` output.
4. **Deferred** — LLM summarization only if (A)–(C) prove insufficient and async cancellation has landed.

**Explicitly avoid** until the above: raising `_MIN_KEEP_RECENT` without the `tool_calls` fix; lowering `recursion_limit` below ~60; total-tool-call hard-refuse guards; migrating code_writer to `create_agent` for middleware.

## References
- Code: `webapp/agents/subagents.py` (`_truncate_messages` 532-617; `_clen` 562; `_trim` 565; `_MIN_KEEP_RECENT` 560; wiring 696-698; `_cap_run_scraper` 894-911; `AGENT_MAX_ITERATIONS` 109; `build_code_writer_message` 2048-3062); `webapp/agents/graph.py` (recursion map 585; `_invoke_code_writer` 2743-2917; `_run_budgeted_agent` 1318; GraphRecursionError caught 104); `webapp/config/settings.py:184-198`.
- Related docs: `docs/code-writer-summarization-plan.md` (half-shipped; SummarizationMiddleware not recommended — see §6); `docs/code-writer-prompt-simplification.md` (shipped); `docs/langgraph-v1-enhancements.md`.
- Memory: `llm-celery-rootcause.md` (Pillar 3c is the live slot; P0 async precondition), `scrape-reliability-rootcause.md`, `aya-mcp-and-api-capture-fixes.md`, `code-writer-context-ballooning-rootcause.md`.
- Commits: `3b5ebf6` (removed headroom from truncator), `7d82d89` (prior headroom-in-truncator), `51521c4` (route_after_testing + admin visibility, deployed).
