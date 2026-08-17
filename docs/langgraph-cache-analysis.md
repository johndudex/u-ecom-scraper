# LangGraph Node-Output Cache — Applicability Analysis

> Status: **Investigated (3 deep agents), verdict NO-GO.** Do not integrate.
> Origin: evaluated `langgraph.cache` (langgraph 1.2.10, already installed) for the scraper-builder pipeline.
> Date: 2026-08-13.

## Executive summary

**Do not adopt LangGraph's node-output Cache.** Three independent deep agents (system map, feature spec, applicability/risk) unanimously concluded it does not fit this system. The planned hybrid (gate rescrape on `nav_changed` + re-hydrate archived `navigation_analysis` into state) solves the only real problem at the correct layer with strictly less risk.

## What the feature is (langgraph 1.2.10, verified from source)

- `compile(cache=<BaseCache>, …)` + per-node `add_node(name, fn, cache_policy=CachePolicy(key_func, ttl))` + graph-wide `set_node_defaults(cache_policy=…)`.
- On a cache **hit** (byte-identical input per `key_func`), the node's Python body is **skipped entirely** and its previously-recorded state-update writes are replayed through the normal merge path (downstream nodes still see the result).
- `CachePolicy.key_func` defaults to `default_cache_key` = `pickle.dumps(_freeze(full_node_input_state))`. Return type `str|bytes`, then `xxh3_128`'d internally.
- Backends: `langgraph.cache.memory.InMemoryCache`, `langgraph.cache.redis.RedisCache(redis_client, prefix="langgraph:cache:")`.
- Stable (non-beta) API. `interrupt()` nodes are never cached. `Command(goto=...)` returns **are** cached.

## Why it doesn't fit here (the three converging reasons)

### 1. The default key essentially never hits
The node input is the full `ScrapeState`. Cross-run: `job_id` differs every job → no hit. Within-run: `test_retry_count`, `coverage_retry_count`, `reanalyze_count`, `remap_count`, `strategies_tried` (`operator.add`), and `test_report` all mutate on every re-entry → no two node invocations share byte-identical state. Any useful caching therefore **requires a custom `key_func`** that projects state down to a node's effective inputs — a large, fragile surface.

> **Correction (from the later channels investigation, see [`langgraph-features-evaluation.md`](./langgraph-features-evaluation.md)):** an earlier draft listed `messages` and `agent_logs` as append-only polluters of the checkpoint. They are in fact **dead in state** — every node returns `{"messages": []}` and the real log path flows to DB (`SessionLog`) + Redis, so the checkpoint holds `[]` for both. This correction doesn't change the verdict: `strategies_tried`, `job_id`, and the retry counters are genuine in-state polluters and still guarantee the default key never hits.

### 2. 22 of 23 nodes are side-effecting → caching is unsafe
A cache hit skips the node body, so file writes / DB calls / subprocess dispatches / browser automation / `interrupt()` are all skipped. On a hit the **state field** comes back but the **artifact on disk does not**:
- `code_writer` writes `input_urls.json`, `discovery_config.json`, `scraper_draft.py` + applies deterministic patches. `setup_workspace` re-hydrates only `scraper_draft.py` (and only when `skip_code_generation` is set) — `discovery_config.json` is **never** re-hydrated, yet the generated scraper reads it at runtime to pick the pagination preset. A hit leaves the restored scraper without its pagination config.
- `code_tester`, `run_execution`, `field_confirmation` run scrapers as subprocesses — non-reproducible.
- The only pure node is `parse_command` (trivial slug computation, runs once) — nothing to save.

### 3. There is no redundant re-execution with identical inputs to dedupe
- **Nothing-changed rescrape:** `code_writer` doesn't run at all — the cascade at `graph.py:1110-1117` + skip flags already skip it. Caching a node that never re-executes adds nothing.
- **Retry loop:** fires *because* `test_report` / `test_retry_count` / `strategies_tried` changed. Include those in the key → always miss (correct). Exclude them → return the same buggy scraper every retry (defeats the retry).
- **code_writer ballooning** is an *intra-node* LLM-loop bug (no summarization, no convergence detection, edit-not-write — see `docs/code-writer-context-ballooning.md`). A node-output cache memoizes at the node boundary; it cannot fix what happens inside one invocation.

## Additional hazards (from the feature spec)

- **Routing nodes are cached.** `Command(goto=...)` returns become cached writes. This graph has many routing nodes (`_accessibility_goto`, `_route_after_site_analyzer`, `route_after_testing`, `route_from_human_approval`). If the field a routing decision depends on is not in the custom key, the wrong branch replays silently.
- **Template/prompt staleness (the killer).** A key projected to `url + page_type` returns a scraper built with an *old* `templates/*.py` / old `code_writer` prompt after a deploy — until TTL drifts. For a deliverable that *is* generated code, that's a correctness regression masquerading as a perf win, with no log signal. Mitigation requires hashing template-file bytes + prompt bytes + `content_type_config` + `output_schema` + `_PATCHES_ENABLED` + `human_feedback` into the key — which makes the key as volatile as the state itself for the retry path (no hits exactly when wanted). **The condition that makes caching safe is the condition that makes it useless here.**
- **Redis backend has no per-job/thread isolation** (key = `prefix:module.qualname:node_name:xxh3`). A weak `key_func` can collide across two different sites that project to the same hash. Must hand-prefix `site_slug`.
- **RedisCache is not truly async** (`aget` calls sync `get`) — blocks the event loop in async graphs (less relevant here; the graph runs sync in Celery).
- **Resume interaction:** a `key_func` that omits `human_feedback`/`human_response` to cut noise would silently ignore user input on a resumed node (`code_writer`, `product_analyzer`, `scraper_analyzer` all appear in `route_from_human_approval`).

## Why the "safe" subset is pointless

The only cache-safe nodes are the deterministic ones — `scraper_analyzer`/`_decide_strategy` and `normalize_fields`. Both are pure-Python, in-process, **millisecond** computations with no LLM call. Caching them saves ~ms while adding a staleness vector *and* a cache-miss penalty (pickling a large state dict is slower than the function). 100% of the wall-clock cost lives in the LLM agents, and every one of those is non-pure, side-effecting, or both.

## Recommendation

**Option (c) — do not adopt.** Pursue the already-staged hybrid instead (`docs/adaptive-pagination-plan.md` "Fix 1 deep critique"):
1. Narrow the rescrape gate from `nav_mode` → `nav_changed` (fresh `browser_traverse` only when nav actually changed).
2. Fix the dead re-hydration: load archived `navigation_analysis.json` into state in `setup_workspace` (nav only) — closes the fields_changed gap at the routing layer.
3. Add a `template_version`/prompt-hash stamp to archived `scraper_analysis.json` so deploy-time staleness is detectable — a single field, far less machinery than a cache-key invalidation subsystem.

This keeps the existing skip-flag system as the single source of truth for "what to re-run," at the routing layer where the decision actually lives. A cache would add a *second, parallel* skip mechanism that can disagree with routing about whether a node ran.

## The one condition to revisit

Cache becomes worth re-evaluating **only if** the architecture moves to **pure, idempotent, side-effect-free decision nodes** separated from side-effecting execution nodes (e.g. a pure "decide strategy" node whose output is fully captured by its state update, with the file writes pushed to a separate node). No such nodes exist today. Until then, LangGraph Cache has no applicable target in this pipeline.

## Key file references
- `webapp/agents/graph.py:4087` — compile site (no cache param today)
- `webapp/agents/state.py:80,157,160` — append-only channels that defeat the default key
- `webapp/agents/nodes/check_tracker.py:68-110` — existing app-level skip system (the correct layer)
- `webapp/agents/graph.py:1110-1117` — the skip cascade (already suppresses the only identical-input re-run)
- `webapp/agents/nodes/setup_workspace.py:131-155` — re-hydration (missing `discovery_config.json`)
- `webapp/agents/graph.py:2752-2926` — code_writer body + side effects (worst cache candidate)
- `docs/adaptive-pagination-plan.md` — the planned hybrid (the recommended alternative)
- `docs/code-writer-context-ballooning.md` — ballooning is intra-node (cache can't help)
