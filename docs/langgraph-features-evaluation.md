# LangGraph Features Evaluation — cache / pregel / channels / supervisor

> Status: **Investigated (4 deep agents across 2 sessions), unanimous NO-GO on adoption.** Current architecture is correct for this system.
> Scope: `langgraph.cache`, `langgraph.pregel`, `langgraph.channels`, `langgraph-supervisor` (langgraph 1.2.10, installed).
> Date: 2026-08-13/14. Detailed cache analysis in [`langgraph-cache-analysis.md`](./langgraph-cache-analysis.md).

## Executive summary

**Do not adopt any of the four.** The system — a fixed ~26-node `StateGraph` with `Command(goto=...)` deterministic routing, each LLM phase a `create_react_agent` wrapped as one node, `PostgresSaver` checkpointer for resume, human-in-the-loop interrupts — is the LangGraph-recommended architecture for a **deterministic, auditable, resumable** pipeline. Every evaluated feature either duplicates app-layer logic already in the right place, is structurally incompatible with the system's constraints, or would break resume/audit correctness.

| Feature | Verdict | One-line reason |
|---|---|---|
| `langgraph.cache` | **NO-GO** | Default key never hits (job_id/counters/test_report mutate); 22/23 nodes side-effecting; no identical-input re-execution exists; ballooning is intra-node. See [cache analysis](./langgraph-cache-analysis.md). |
| `langgraph-supervisor` | **NO-GO** | Pipeline routing is 100% rule-based by design; supervisor trades determinism/auditability/resumability for flexibility the system doesn't need and would break the HIP resume contract. |
| `langgraph.pregel` (untapped) | **NO-GO** | Retry/timeout/step-budget/observability all already implemented at the app layer where they can act on real (intra-node, sync, subprocess) failures; `Send` fan-out incompatible with shared-Chrome/subprocess extraction on anti-bot sites. |
| `langgraph.channels` (custom) | **NO-GO** | `EphemeralValue`/`UntrackedValue` either break resume or save <1KB; `Topic` is in-process vs the cross-process Redis pub-sub already in use; no fork/join for barriers; `DeltaChannel` is BETA. |

## Per-feature reasoning (compressed from agent reports)

### langgraph-supervisor — NO-FIT (whole or sub-scoped)
Supervisor = an LLM decides which worker to call next, in a loop. It shines for **open-ended, conversational, dynamic** multi-agent coordination. This pipeline's stage order (probe → analyze → generate → test → execute) is a **domain invariant**, and every routing decision is a deterministic function of inspectable state (`input_mode`, skip flags, `test_report` fields, confidence) — zero LLM routing today, by deliberate design (the retry cascade at `route_after_testing.py` carries comments documenting that deterministic guards were added *to suppress prior bugs from LLM subjectivity*). Sub-candidates all fail: the retry loop is deterministic-by-experience; navigation is single-agent tool-use (not multi-worker); site-understanding is already decomposed via the skills-as-tools system. **Biggest risk:** non-deterministic resume — `route_from_human_approval` maps an approval's `interrupt_reason` to a fixed node target; a supervisor re-deciding routing post-resume voids the meaning of a human's approval.

### langgraph.pregel — every untapped capability already covered at the app layer
- **`Send`/map-reduce:** the per-item extraction that *looks* parallelizable runs **inside one generated subprocess** (`run_execution` dispatches a single `scraper_draft.py`) against a **shared Chrome** on anti-bot sites — sequential extraction is a deliberate stealth/politeness choice. Parallel probes would always fire the costly residential-proxy tier (spend regression).
- **`RetryPolicy`:** only fires on *unhandled exceptions*; agent nodes never raise (`_invoke_agent_with_timeout` catches all → `{"messages": []}`). The real failures (900s timeouts, OOM, intra-node ballooning) aren't retryable exceptions.
- **Recursion/step budget:** already correct at the inner-react layer (`AGENT_RECURSION_MAP`, `code_writer:120`) + auto-extend + 900s wall-clock cap. No pregel primitive improves on it.
- **`TimeoutPolicy`:** the one conditional GO — *after* the planned async-agent migration (`LLM_ASYNC_EXECUTION`), it could **replace** (not duplicate) `_invoke_agent_with_timeout` for uniformity. Today, with sync nodes, it can't interrupt the in-flight socket (leak-prone) and raises `NodeTimeoutError` (crash vs the current graceful budget-interrupt). Consolidation only, not new capability.
- **Streaming/observability:** the current per-tool-call `_ToolCallLogger` + heartbeats are *more granular* than pregel's node-boundary stream events.

### langgraph.channels — no type clears the bar
Semantics verified from source (several subtler than docs):
- **`EphemeralValue`** IS checkpointed and auto-clears on the next non-writing step → would silently drop artifacts (`site_analysis`, etc.) that downstream nodes read across many super-steps and interrupts. **Breaks resume.**
- **`UntrackedValue`** (truly un-checkpointed): only safe for the two *dead* fields (`messages`, `agent_logs`) — saving **<1 KB total**. Every live field must survive interrupts.
- **`Topic`:** in-process only + checkpointed; the live-log streaming here is **cross-process** (Celery → Django SSE + watchdog) and already cleanly handled by Redis pub/sub + DB. `Topic` would duplicate, not replace.
- **`NamedBarrierValue`:** the graph is strictly linear with conditional routing — no fork/join to synchronize.
- **`DeltaChannel`:** BETA; targets large append-only reducers, but the only live one (`strategies_tried`) is tiny; the big state (artifact blobs) uses last-write-wins.

**Re-framing from the channels investigation (corrects the cache doc):** `messages` and `agent_logs` are **dead in state** — every node returns `{"messages": []}` and the real logging flows to DB (`SessionLog`) + Redis (`job:{job_id}`), bypassing parent state. So the cache analysis's "append-only pollution" was right about `strategies_tried`/`job_id`/counters but imprecise about those two (they pollute the DB/Redis, not the checkpoint, which holds `[]`). The cache verdict is unaffected — the other polluters are real and in state.

## Orthogonal cleanups surfaced (NOT feature adoptions; optional backlog)

These are minor code-quality items the agents flagged while investigating. None require LangGraph feature adoption; none warrant the multi-iteration planning pipeline. Listed for backlog, lowest-risk first:

1. **Remove dead `ScrapeState` fields** — `messages` (`state.py:157`), `agent_logs` (`state.py:160`), and the never-referenced counters (`content_analysis_retries`, `site_budget_retries`, `product_budget_retries`, `nav_budget_retries`). Plain TypedDict cleanup + drop the `add_messages` import + drop the `{"messages": []}` returns. Shrinks the schema surface and removes a trap for future devs. Note: `messages` may be required by langgraph internals for the react sub-agents — verify the parent-state field is truly separable from the inner-agent message channel before removing.
2. **Rewrite `browser_traverse`'s hand-rolled while-loop** (`experimental/nav_traversal/traversal.py:1796`) as a `create_react_agent` — aligns it with every other LLM phase; removes manual `llm.invoke` + JSON-parse + step-counting. Local readability win, not an architecture change. Medium effort.
3. **After the async-agent migration lands**, consider replacing `_invoke_agent_with_timeout` (graph.py:1277) with per-node `add_node(..., timeout=TimeoutPolicy(run_timeout=900))` — consolidation only, gated on `LLM_ASYNC_EXECUTION` being default + regression-free.

## When to revisit

- **Cache / channels:** only if the architecture introduces **pure, idempotent, side-effect-free decision nodes** separated from side-effecting execution nodes. None exist today.
- **Supervisor:** only if the product pivots to a genuinely **open-ended conversational agent** where "what to do next" is itself the unknown (freeform "scrape anything" assistant). Not this system.
- **`Send` fan-out:** only if extraction moves out of a single subprocess into per-item graph nodes AND anti-bot constraints allow concurrency.

## Key file references
- `webapp/agents/graph.py:4087` — compile (no cache/store/supervisor today)
- `webapp/agents/graph.py:3928-3961` — bare `add_node` calls (no retry/timeout/cache_policy)
- `webapp/agents/graph.py:1110-1117, 3760-3881` — deterministic Command routing + resume cascade (why supervisor breaks things)
- `webapp/agents/graph.py:1216-1315` — `_invoke_agent_with_timeout` (900s cap; why RetryPolicy/TimeoutPolicy are redundant today)
- `webapp/agents/nodes/run_execution.py:183` — single-subprocess dispatch (why `Send` doesn't apply)
- `webapp/agents/state.py:80,157,160` — `strategies_tried` (live append-only) + `messages`/`agent_logs` (dead in state)
- `webapp/agents/nodes/route_after_testing.py:375-653` — deterministic retry cascade (the strongest supervisor test case, still NO-FIT)
- `experimental/nav_traversal/traversal.py:1796` — hand-rolled browser loop (cleanup candidate #2)
