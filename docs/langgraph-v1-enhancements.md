# LangGraph v1 Enhancements — What's Available + What We Should Use

**Context:** the langgraph upgrade (0.6 → 1.2.7) is done (see `langgraph-upgrade-plan.md`).
This doc maps the **new v1 capabilities** to the **known problems** in the system
(learned from running jobs 228–242) and recommends what to adopt, what to defer, and why.

The key v1 addition is the **`create_agent` middleware system** (replacing the
deprecated `create_react_agent`). Middleware lets us run deterministic code
*around* each agent's model/tool calls — exactly where most of our pain lives.

---

## 1. The known problems (evidence-based)

| # | Problem | Current handling | Cost |
|---|---|---|---|
| P1 | **code_writer variance** — wrong strategy (SeleniumBase vs playwright), crashes (`--xvfb`), free-form drift | `route_after_testing` retry loop; LLM-described remediation; just-added Fix A/B/C | many wasted retries; sometimes exhausts → human_approval |
| P2 | **Tool errors crash agents** — v1's ToolNode *re-raises* tool exceptions (v0.6 swallowed them) | hand-patched `_parse_input` (`_strip_v_prefix`) + `_coerce_url_str` guard | each unmigrated tool = a latent crash |
| P3 | **Message/context bloat** — long agent runs overflow context | custom `_truncate_messages` (`pre_model_hook`) + `headroom.compress` | works, but hand-rolled + per-agent |
| P4 | **HITL plumbing** — interrupt → approval mapping → resume | custom `interrupt()` + `INTERRUPT_TO_APPROVAL_TYPE` + `_check_and_create_approval` + `Command(resume=…)` | large surface, hand-maintained |
| P5 | **Recursion limit** — agents looping hit the per-agent cap | raised `AGENT_RECURSION_MAP` + graceful `GraphRecursionError → approval` | treats the symptom |
| P6 | **Unstructured agent outputs** — analyses/test_report are LLM-written JSON, parsed defensively | `_load_test_report`, `_fix_json_artifact`, `read_json_artifact` with fallbacks | fragile; the code_tester "remediation" can contradict the chosen strategy (Fix C) |

---

## 2. v1 capabilities we can now use

| v1 feature | What it does | Replaces in our code |
|---|---|---|
| **`create_agent` + middleware** | agents accept a `middleware=[…]` list; each middleware has `before_model`/`after_model`/`wrap_tool_call` hooks | ad-hoc `pre_model_hook`, in-node guards, manual tool wrapping |
| **`wrap_tool_call` middleware** | intercept any tool exception → return a retry message (restores v0.6 behavior generically) | the `_parse_input`/`_coerce_url_str` patches (P2) |
| **built-in summarization middleware** | trim/summarize message history each round | `_truncate_messages` + `headroom.compress` (P3) |
| **`before_model` / `after_model` middleware** | deterministic pre/post model hooks (guardrails, validation) | Fix B (strategy guard) in `_invoke_code_tester`; Fix C in the message (P1, P5) |
| **structured output (`ToolStrategy`/`ProviderStrategy`)** | force the agent to emit typed, validated JSON | hand-parsed `test_report`/`*_analysis.json` + `_fix_json_artifact` (P6) |
| **`context` arg** | typed runtime context injected into tools/agents (replaces `config["configurable"]`) | `set_tool_context` thread-locals (P-architecture) |
| **built-in HITL middleware** | first-class interrupt/request/approve flow | custom approval mapping + resume plumbing (P4) |
| **standard content blocks** | provider-agnostic message content | n/a yet, but enables multi-provider cleanly |
| **`dynamic_prompt` / dynamic model middleware** | adapt prompt/model per call | message builders re-deriving context each call |

---

## 3. Recommended adoptions (ranked by leverage vs effort)

### 🟢 Adopt soon — high value, low risk

**A. `wrap_tool_call` middleware → generic tool-error handling (fixes P2 properly).**
Today we hand-patch each tool that could crash under v1 (`_parse_input`, `_coerce_url_str`).
A single `wrap_tool_call` middleware that catches `Exception` and returns a structured
"tool error: …" message restores v0.6's forgiving behavior for **every** tool, present and
future. This is the single highest-leverage v1 adoption — it eliminates a whole class of
latent crashes without per-tool patches.
*Effort:* small (one middleware). *Caution:* migrating an agent to `create_agent` is
required to attach middleware — see §4.

**B. `after_model` middleware on code_writer → strategy guard as middleware (cleaner P1).**
Our Fix B (`_check_strategy_mismatch` in `_invoke_code_tester`) works, but it's bolted onto
the node. As an `after_model` middleware on `code_writer`, the strategy check runs the moment
code_writer finishes writing — earlier in the loop, before testing. Same idea, better place.
Pairs with Fix A (crash-aware) + Fix C.

**C. Structured output for `test_report` + analyses (fixes P6 at the source).**
The code_tester's `remediation` mis-leds because it's free-form LLM JSON. A `ToolStrategy`
that forces `test_report` into a typed schema (`overall_assessment: Literal[…]`,
`remediation.target: Literal["scraper","mapping"]`, `crash_error: Optional[str]`) makes the
output machine-validatable → the router never sees malformed/contradictory remediation.
*Effort:* medium (define schemas). *Value:* removes a whole class of routing bugs.

### 🟡 Adopt medium-term — solid value, more effort

**D. Built-in summarization middleware (replaces P3 plumbing).**
`_truncate_messages` + `headroom.compress` work but are hand-rolled per agent. v1's
summarization middleware is maintained + tunable. Migrate once per heavy agent
(site_analyzer, product_analyzer, code_writer — the ones that overflow).

**E. `before_model` middleware for guardrails (P1, P5).**
Strategy re-anchoring ("you must use strategy X"), budget countdown ("N calls left, write
the artifact now"), and PII/log hygiene fit naturally as `before_model` hooks — cleaner
than re-deriving them in each `build_*_message`.

### 🔴 Defer / evaluate carefully

**F. Full `create_react_agent → create_agent` migration.**
The catch: middleware requires `create_agent`, but `create_agent` currently **can't rewrite
message history as a function of state** (the v1 migration guide's stated gap). Our
`_truncate_messages`/`headroom` pipeline depends on exactly that. So:
- **Don't** blindly migrate all agents.
- **Do** migrate agent-by-agent where (a) middleware benefits are high (code_tester,
  code_writer — for A/B/C/E) AND (b) the agent doesn't need state-driven message rewriting.
  code_tester is the ideal first candidate (it doesn't truncate history).

**G. HITL middleware (P4).**
Our custom interrupt→approval→resume works and is deeply wired into Django models + SSE.
The v1 HITL middleware is cleaner but migrating it touches a lot. Defer unless reworking
the approval UI.

**H. `context` arg replacing thread-local tool context.**
`set_tool_context` (thread-local) works but is fragile (concurrency, testability). v1's
`context` is typed + injected. Worth doing eventually for correctness, but it's a wide
refactor of `agents/tools/context.py` + every tool. Defer.

---

## 4. The migration constraint (important)

**Middleware is a `create_agent` feature, not `create_react_agent`.** We deliberately kept
`create_react_agent` (deprecated but functional) because `create_agent` has the
message-rewriting gap. So **each adoption above that needs middleware (A, B, D, E) requires
migrating that specific agent to `create_agent` first.**

Recommended order:
1. Migrate **code_tester** → `create_agent` first (no history-rewriting needed). Attach
   `wrap_tool_call` (A) + structured `test_report` output (C). Immediate payoff for P1/P2/P6.
2. Migrate **code_writer** → `create_agent` + `after_model` strategy guard (B). Watch for
   the history-rewriting gap (code_writer does run truncation) — may need a middleware-based
   summarizer (D) instead of `_truncate_messages`.
3. Leave site_analyzer/navigation/product_analyzer on `create_react_agent` until the gap
   closes OR summarization middleware (D) replaces their truncation.

---

## 5. What NOT to do
- Don't hardcode site-specific logic (generic-over-deterministic rule). v1 features should
  encode *generic* constraints (strategy rules, tool-error handling) — not calvklein selectors.
- Don't migrate all agents at once — the message-rewriting gap will bite the heavy agents.
- Don't adopt `context`/HITL middleware as a refactor for its own sake — only if it fixes a
  live problem.

---

## 6. Summary
The upgrade's biggest practical win is **middleware around agents** — it lets us move our
hard-won deterministic fixes (strategy guard, crash routing, tool-error handling, message
trimming) from scattered node/message code into clean, reusable per-agent hooks. Start with
**code_tester** (`wrap_tool_call` + structured `test_report`) — it's the safest first
`create_agent` migration and directly attacks the variance/retry problem (P1/P2/P6).
